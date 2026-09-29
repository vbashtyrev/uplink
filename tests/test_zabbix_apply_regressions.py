"""Regression tests: plan/apply alignment, Zabbix cache, dashboard errors."""

import json
from pathlib import Path
from unittest.mock import patch

import zabbix_provider_aggregate as agg
import zabbix_provider_services as svc

from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from tests.test_zabbix_uplinks_plan import _activate_plan_mocker
from uplinks.zabbix.client import (
    load_zabbix_hosts_and_items_for_scope,
    save_zabbix_cache,
)
from uplinks.zabbix.plan import DEFAULT_PARENT_SERVICE, build_zabbix_plan
from uplinks_config import TRIGGER_DESC_100_SUFFIX
from zabbix_sync_commit_rate import prune_burst_link_triggers_on_host
import zabbix_uplinks_dashboard as dash

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_plan_burst_delete_when_interface_moves(monkeypatch, zabbix_env, tmp_path):
    inv = tmp_path / "inv.json"
    inv.write_text(
        json.dumps(
            {
                "complete": [
                    {
                        "device": "R1",
                        "interface": "Ethernet52/1",
                        "provider": "Cogent",
                        "billing_model": "Burst",
                        "circuit_id": "CKT-1",
                        "commit_rate_bps": 10_000_000_000,
                    }
                ],
                "incomplete": [],
                "stats": {"complete": 1, "providers": 1, "providers_in_scope": 1},
                "provider_slo_percent": {"Cogent": 99.9},
                "provider_limits_gbps": {"Cogent": 10},
                "provider_slo_read": "ok",
                "provider_limits_read": "ok",
                "netbox_interface_relations": {},
            }
        ),
        encoding="utf-8",
    )
    old_desc = "Interface Ethernet51/1: {}".format(TRIGGER_DESC_100_SUFFIX)

    def host_get(params):
        if "R1" in (params.get("filter") or {}).get("host", []):
            return [{"hostid": "10", "host": "R1", "name": "R1"}]
        return []

    def item_get(params):
        if (params.get("search") or {}).get("name") == "Bits received":
            return [
                {
                    "key_": "net.if.in[Eth52/1]",
                    "name": "Interface Ethernet52/1: Bits received",
                }
            ]
        return []

    def trigger_get(params):
        search = (params.get("search") or {}).get("description", "")
        if search == "Interface Ethernet52/1:":
            return []
        if search == "Interface ":
            return [
                {
                    "triggerid": "900",
                    "description": old_desc,
                    "tags": [{"tag": "billing", "value": "burst"}],
                }
            ]
        return []

    _activate_plan_mocker(
        monkeypatch,
        host_get=host_get,
        item_get=item_get,
        trigger_get=trigger_get,
    )

    report, err = build_zabbix_plan(
        None,
        inventory_file=str(inv),
        create_link_triggers=True,
    )
    assert err is None
    deletes = report["planned"]["burst_triggers"]["delete"]
    assert any(d.get("triggerid") == "900" for d in deletes)


def test_prune_burst_triggers_on_interface_move(monkeypatch):
    old_desc = "Interface Ethernet51/1: {}".format(TRIGGER_DESC_100_SUFFIX)
    deleted = []

    (
        ZabbixRpcMocker()
        .on(
            "trigger.get",
            lambda p: [
                {
                    "triggerid": "900",
                    "description": old_desc,
                    "tags": [{"tag": "billing", "value": "burst"}],
                }
            ],
        )
        .on("trigger.delete", lambda p: deleted.extend(p) or True)
        .activate(monkeypatch)
    )

    count, err = prune_burst_link_triggers_on_host(
        "https://z.example/api_jsonrpc.php",
        "t",
        "10",
        {"ethernet52/1"},
    )
    assert err is None
    assert count == 1
    assert deleted == ["900"]


def test_services_apply_deletes_orphan_service_and_sla(monkeypatch, zabbix_env):
    ctx = {
        "providers": ["Cogent"],
        "burst_circuits": [],
        "provider_slo_percent": {"Cogent": 99.9},
        "provider_slo_read": "ok",
        "read_error": False,
        "report": {"provider_slo_read": "ok"},
    }
    deleted_services = []
    deleted_slas = []

    def service_get(params):
        search = (params.get("search") or {}).get("name", "")
        if search == "Uplinks":
            return [
                {"serviceid": "1", "name": "Uplinks Cogent"},
                {"serviceid": "2", "name": "Uplinks Orphan"},
            ]
        filt = (params.get("filter") or {}).get("name") or []
        rows = []
        if "Uplinks Cogent" in filt:
            rows.append({"serviceid": "1", "name": "Uplinks Cogent", "parents": []})
        return rows

    def sla_get(params):
        search = (params.get("search") or {}).get("name", "")
        if search == "Uplinks":
            return [
                {"slaid": "10", "name": "Uplinks Cogent SLA"},
                {"slaid": "11", "name": "Uplinks Orphan SLA"},
            ]
        filt = (params.get("filter") or {}).get("name") or []
        if "Uplinks Cogent SLA" in filt:
            return [{"slaid": "10", "name": "Uplinks Cogent SLA", "slo": "99.9"}]
        return []

    (
        ZabbixRpcMocker()
        .on("service.get", service_get)
        .on("service.update", lambda p: True)
        .on("sla.get", sla_get)
        .on("sla.update", lambda p: True)
        .on("service.delete", lambda p: deleted_services.extend(p) or True)
        .on("sla.delete", lambda p: deleted_slas.extend(p) or True)
        .activate(monkeypatch)
    )

    err = svc._delete_orphan_uplinks_services_and_slas(
        "https://z.example/api_jsonrpc.php",
        "t",
        ["Cogent"],
        [],
        ctx,
        project_slo=99.95,
    )
    assert err is None
    assert "2" in deleted_services
    assert "11" in deleted_slas


def test_orphan_cleanup_keeps_sla_with_project_default_slo(monkeypatch, zabbix_env):
    ctx = {
        "providers": ["ManualISP"],
        "burst_circuits": [],
        "provider_slo_percent": {},
        "provider_slo_read": "ok",
        "read_error": False,
        "report": {"provider_slo_read": "ok"},
    }
    deleted_slas = []

    def sla_get(params):
        search = (params.get("search") or {}).get("name", "")
        if search == "Uplinks":
            return [{"slaid": "20", "name": "Uplinks ManualISP SLA"}]
        return []

    (
        ZabbixRpcMocker()
        .on("service.get", lambda p: [])
        .on("sla.get", sla_get)
        .on("sla.delete", lambda p: deleted_slas.extend(p) or True)
        .activate(monkeypatch)
    )

    err = svc._delete_orphan_uplinks_services_and_slas(
        "https://z.example/api_jsonrpc.php",
        "t",
        ["ManualISP"],
        [],
        ctx,
        project_slo=99.95,
    )
    assert err is None
    assert deleted_slas == []


def test_orphan_cleanup_preserves_default_parent_service(monkeypatch, zabbix_env):
    deleted_services = []
    ctx = {
        "providers": ["Cogent"],
        "burst_circuits": [],
        "provider_slo_percent": {"Cogent": 99.9},
        "provider_slo_read": "ok",
        "report": {"provider_slo_read": "ok"},
    }

    def service_get(params):
        search = (params.get("search") or {}).get("name", "")
        if search == "Uplinks":
            return [
                {"serviceid": "801", "name": DEFAULT_PARENT_SERVICE},
                {"serviceid": "1", "name": "Uplinks Cogent"},
                {"serviceid": "2", "name": "Uplinks Orphan"},
            ]
        return []

    (
        ZabbixRpcMocker()
        .on("service.get", service_get)
        .on("service.delete", lambda p: deleted_services.extend(p) or True)
        .on("sla.get", lambda p: [])
        .activate(monkeypatch)
    )

    err = svc._delete_orphan_uplinks_services_and_slas(
        "https://z.example/api_jsonrpc.php",
        "t",
        ["Cogent"],
        [],
        ctx,
        project_slo=99.95,
    )
    assert err is None
    assert "801" not in deleted_services
    assert "2" in deleted_services


def test_plan_burst_stale_cleanup_when_host_has_no_burst_scope(
    monkeypatch, zabbix_env, tmp_path
):
    inv = tmp_path / "inv.json"
    inv.write_text(
        json.dumps(
            {
                "complete": [
                    {
                        "device": "R1",
                        "interface": "Ethernet52/1",
                        "provider": "Cogent",
                        "billing_model": "Flat",
                        "circuit_id": "CKT-FLAT",
                        "commit_rate_bps": 10_000_000_000,
                    }
                ],
                "incomplete": [],
                "stats": {"complete": 1, "providers": 1, "providers_in_scope": 1},
                "provider_slo_percent": {"Cogent": 99.9},
                "provider_limits_gbps": {"Cogent": 10},
                "provider_slo_read": "ok",
                "provider_limits_read": "ok",
                "netbox_interface_relations": {},
            }
        ),
        encoding="utf-8",
    )
    old_desc = "Interface Ethernet51/1: {}".format(TRIGGER_DESC_100_SUFFIX)

    def host_get(params):
        if "R1" in (params.get("filter") or {}).get("host", []):
            return [{"hostid": "10", "host": "R1", "name": "R1"}]
        return []

    def item_get(params):
        if (params.get("search") or {}).get("name") == "Bits received":
            return [
                {
                    "key_": "net.if.in[Eth52/1]",
                    "name": "Interface Ethernet52/1: Bits received",
                }
            ]
        return []

    def trigger_get(params):
        search = (params.get("search") or {}).get("description", "")
        if search == "Interface ":
            return [
                {
                    "triggerid": "901",
                    "description": old_desc,
                    "tags": [{"tag": "billing", "value": "burst"}],
                }
            ]
        return []

    _activate_plan_mocker(
        monkeypatch,
        host_get=host_get,
        item_get=item_get,
        trigger_get=trigger_get,
    )

    report, err = build_zabbix_plan(
        None,
        inventory_file=str(inv),
        create_link_triggers=True,
    )
    assert err is None
    deletes = report["planned"]["burst_triggers"]["delete"]
    assert any(d.get("triggerid") == "901" for d in deletes)


def test_prune_burst_triggers_with_empty_allowed_scope(monkeypatch):
    old_desc = "Interface Ethernet51/1: {}".format(TRIGGER_DESC_100_SUFFIX)
    deleted = []

    (
        ZabbixRpcMocker()
        .on(
            "trigger.get",
            lambda p: [
                {
                    "triggerid": "902",
                    "description": old_desc,
                    "tags": [{"tag": "billing", "value": "burst"}],
                }
            ],
        )
        .on("trigger.delete", lambda p: deleted.extend(p) or True)
        .activate(monkeypatch)
    )

    count, err = prune_burst_link_triggers_on_host(
        "https://z.example/api_jsonrpc.php",
        "t",
        "10",
        set(),
    )
    assert err is None
    assert count == 1
    assert deleted == ["902"]


def test_aggregate_refetch_only_known_zabbix_hosts(tmp_path, monkeypatch):
    dry_ssh = FIXTURES / "dry_ssh_minimal.json"
    desc_map = tmp_path / "description_to_name.json"
    desc_map.write_text(
        json.dumps({"Uplink: Cogent 10G": "Cogent", "Uplink: Hurricane": "Hurricane"}),
        encoding="utf-8",
    )
    cache_path = tmp_path / "cache.json"
    save_zabbix_cache(
        str(cache_path),
        {"ALA-KZT-7280TR-1": "101"},
        {},
    )
    fetch_hosts = []

    def fake_fetch(url, token, hostnames, debug=False):
        fetch_hosts.extend(sorted(hostnames))
        if "FRN-MX-1" in hostnames:
            return None, None, "hosts not found in Zabbix: FRN-MX-1"
        return (
            {"ALA-KZT-7280TR-1": "101"},
            {
                ("ALA-KZT-7280TR-1", "ethernet51/1"): {
                    "itemid_in": "1",
                    "itemid_out": "2",
                    "bits_in": "a",
                    "bits_out": "b",
                }
            },
            None,
        )

    mocker = (
        ZabbixRpcMocker()
        .on("user.get", lambda p: [{"userid": "1"}])
        .on("hostgroup.get", lambda p: [{"groupid": "2"}])
        .on(
            "host.get",
            lambda p: [
                {"hostid": "101", "host": h, "name": h}
                for h in (p.get("filter", {}).get("host") or [])
                if h == "ALA-KZT-7280TR-1"
            ],
        )
        .on("host.create", lambda p: {"hostids": ["999"]})
        .on("item.get", lambda p: [])
        .on("item.create", lambda p: {"itemids": ["i1"]})
        .on("item.update", lambda p: True)
        .on("trigger.get", lambda p: [])
        .on("trigger.create", lambda p: {"triggerids": ["t1"]})
        .on("trigger.update", lambda p: True)
    )
    mocker.activate(monkeypatch)

    nb_ctx = {
        "device_iface_to_provider": {
            ("ALA-KZT-7280TR-1", "ethernet51/1"): "Cogent",
            ("FRN-MX-1", "ae5.0"): "Hurricane",
        },
        "providers": {"Cogent", "Hurricane"},
        "provider_limits_gbps": {"Cogent": 10, "Hurricane": 5},
        "stats": {},
    }

    with patch.object(agg, "_load_netbox_aggregate_context", return_value=nb_ctx):
        with patch.object(agg, "fetch_zabbix_hosts_and_items", side_effect=fake_fetch):
            done, err = agg.run(
                "https://z.example/api_jsonrpc.php",
                "token",
                str(dry_ssh),
                str(desc_map),
                cache_path=str(cache_path),
                debug=False,
            )

    assert err is None
    assert done
    assert fetch_hosts == ["ALA-KZT-7280TR-1"]


def test_cache_refetches_when_required_pair_missing(monkeypatch, zabbix_env, tmp_path):
    cache_path = tmp_path / "cache.json"
    save_zabbix_cache(
        str(cache_path),
        {"R1": "10"},
        {
            ("R1", "ethernet51/1"): {
                "itemid_in": "1",
                "itemid_out": "2",
                "bits_in": "a",
                "bits_out": "b",
            }
        },
    )
    fetch_hosts = []

    def fake_fetch(url, token, hostnames, debug=False):
        fetch_hosts.extend(sorted(hostnames))
        items = {
            ("R1", "ethernet52/1"): {
                "itemid_in": "99",
                "itemid_out": "100",
                "bits_in": "k-in",
                "bits_out": "k-out",
            }
        }
        return {"R1": "10"}, items, None

    monkeypatch.setattr(
        "uplinks.zabbix.client.fetch_zabbix_hosts_and_items",
        fake_fetch,
    )

    host_id, items, err = load_zabbix_hosts_and_items_for_scope(
        "https://z.example/api_jsonrpc.php",
        "t",
        {"R1"},
        required_pairs={("R1", "ethernet52/1")},
        cache_path=str(cache_path),
        no_cache=False,
    )
    assert err is None
    assert fetch_hosts == ["R1"]
    assert items[("R1", "ethernet52/1")]["itemid_in"] == "99"


def test_dashboard_create_update_get_errors(monkeypatch):
    edges = [
        ("R1", "1", "Eth1", "Cogent", "10", "11", "k1", "k2", "desc"),
    ]

    (
        ZabbixRpcMocker()
        .on(
            "dashboard.get",
            lambda p: (_ for _ in ()).throw(RuntimeError("dashboard.get: denied")),
        )
        .activate(monkeypatch)
    )
    _, err_get = dash.create_or_update_dashboard(
        "https://z.example/api_jsonrpc.php",
        "t",
        edges,
        "Uplinks test",
    )
    assert err_get is not None
    assert "dashboard.get" in err_get

    (
        ZabbixRpcMocker()
        .on(
            "dashboard.get",
            lambda p: [{"dashboardid": "5", "name": "Uplinks test", "pages": []}],
        )
        .on(
            "dashboard.update",
            lambda p: (_ for _ in ()).throw(RuntimeError("dashboard.update: fail")),
        )
        .activate(monkeypatch)
    )
    _, err_update = dash.create_or_update_dashboard(
        "https://z.example/api_jsonrpc.php",
        "t",
        edges,
        "Uplinks test",
    )
    assert err_update is not None
    assert "dashboard.update" in err_update

    (
        ZabbixRpcMocker()
        .on("dashboard.get", lambda p: [])
        .on(
            "dashboard.create",
            lambda p: (_ for _ in ()).throw(RuntimeError("dashboard.create: fail")),
        )
        .activate(monkeypatch)
    )
    _, err_create = dash.create_or_update_dashboard(
        "https://z.example/api_jsonrpc.php",
        "t",
        edges,
        "Uplinks test",
    )
    assert err_create is not None
    assert "dashboard.create" in err_create

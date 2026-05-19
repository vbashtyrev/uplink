"""Push zabbix_provider_aggregate to >=85%."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from uplinks_config import UPLINKS_AGGREGATE_HOST_PREFIX
import zabbix_provider_aggregate as agg

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_ensure_triggers_update_cleanup_and_dependency(monkeypatch):
    desc_warn = "Provider aggregate traffic >= 90% of limit (10.0 Gbps)"
    desc_high = "Provider aggregate traffic >= 100% of limit (10.0 Gbps)"
    desc_sla = "Provider aggregate SLA breach: >= 100% of limit for 15m (10.0 Gbps)"
    triggers = [
        {"triggerid": "old-warn", "description": desc_warn + " (old)"},
        {"triggerid": "keep-warn", "description": desc_warn},
        {"triggerid": "keep-high", "description": desc_high},
        {"triggerid": "old-sla", "description": desc_sla + " x"},
        {"triggerid": "keep-sla", "description": desc_sla},
    ]
    deleted = []
    updates = []

    def trigger_update(params):
        updates.append(params)
        return True

    def trigger_delete(params):
        deleted.extend(params if isinstance(params, list) else [params])
        return True

    (
        ZabbixRpcMocker()
        .on("trigger.get", lambda p: triggers)
        .on("trigger.update", trigger_update)
        .on("trigger.create", lambda p: {"triggerids": ["new"]})
        .on("trigger.delete", trigger_delete)
        .activate(monkeypatch)
    )
    err = agg._ensure_triggers(
        "https://z.example/api_jsonrpc.php",
        "t",
        "100",
        "Uplinks-Cogent",
        "Cogent",
        None,
        10e9,
    )
    assert err is None
    assert "old-warn" in deleted or "old-sla" in deleted
    assert any(u.get("dependencies") for u in updates)


def test_delete_provider_aggregate_triggers(monkeypatch):
    (
        ZabbixRpcMocker()
        .on(
            "trigger.get",
            lambda p: [{"triggerid": "1", "description": "Provider aggregate x"}],
        )
        .on("trigger.delete", lambda p: True)
        .activate(monkeypatch)
    )
    assert agg._delete_provider_aggregate_triggers("https://z.example/api_jsonrpc.php", "t", "1") is None


def test_get_or_create_host_errors(monkeypatch):
    (
        ZabbixRpcMocker()
        .on("hostgroup.get", lambda p: [])
        .activate(monkeypatch)
    )
    hid, err = agg._get_or_create_host("https://z.example/api_jsonrpc.php", "t", "Uplinks X", "Uplinks")
    assert hid is None
    assert "group not found" in err

    (
        ZabbixRpcMocker()
        .on("hostgroup.get", lambda p: [{"groupid": "2"}])
        .on("host.get", lambda p: (_ for _ in ()).throw(RuntimeError("host get fail")))
        .activate(monkeypatch)
    )
    hid, err = agg._get_or_create_host("https://z.example/api_jsonrpc.php", "t", "Uplinks X", "Uplinks")
    assert hid is None
    assert "Zabbix API" in err


def test_get_providers_netbox_debug_names(capsys, monkeypatch):
    nb = MagicMock()
    nb.circuits.providers.filter.return_value = [type("P", (), {"name": "Cogent"})()]
    with patch("zabbix_provider_aggregate.pynetbox.api", return_value=nb):
        monkeypatch.setenv("NETBOX_URL", "https://nb.example")
        monkeypatch.setenv("NETBOX_TOKEN", "tok")
        names = agg._get_providers_from_netbox("automatization", debug=True)
    assert names == ["Cogent"]
    assert "Cogent" in capsys.readouterr().err


def test_get_or_create_host_create_error(monkeypatch):
    (
        ZabbixRpcMocker()
        .on("hostgroup.get", lambda p: [{"groupid": "2"}])
        .on("host.get", lambda p: [])
        .on("host.create", lambda p: (_ for _ in ()).throw(RuntimeError("create fail")))
        .activate(monkeypatch)
    )
    hid, err = agg._get_or_create_host("https://z.example/api_jsonrpc.php", "t", "Uplinks X", "Uplinks")
    assert hid is None
    assert "host.create" in err


def test_create_calculated_item_errors(monkeypatch):
    (
        ZabbixRpcMocker()
        .on("item.get", lambda p: (_ for _ in ()).throw(RuntimeError("item get")))
        .activate(monkeypatch)
    )
    iid, err = agg._create_or_update_calculated_item(
        "https://z.example/api_jsonrpc.php", "t", "1", "k", "n", "f"
    )
    assert iid is None
    assert "item get" in err or "Zabbix API" in err

    (
        ZabbixRpcMocker()
        .on("item.get", lambda p: [])
        .on("item.create", lambda p: (_ for _ in ()).throw(RuntimeError("item create")))
        .activate(monkeypatch)
    )
    iid, err = agg._create_or_update_calculated_item(
        "https://z.example/api_jsonrpc.php", "t", "1", "k", "n", "f"
    )
    assert iid is None
    assert "item create" in err or "Zabbix API" in err


def test_create_calculated_item_create_path(monkeypatch):
    (
        ZabbixRpcMocker()
        .on("item.get", lambda p: [])
        .on("item.create", lambda p: {"itemids": ["77"]})
        .activate(monkeypatch)
    )
    iid, err = agg._create_or_update_calculated_item(
        "https://z.example/api_jsonrpc.php",
        "t",
        "1",
        "aggregate.bits.in[]",
        "in",
        "sum(//x)",
    )
    assert err is None
    assert iid == "77"


def test_run_prune_triggers_without_limit(monkeypatch, tmp_path):
    desc = tmp_path / "desc.json"
    desc.write_text(json.dumps({"Uplink: Cogent 10G": "Cogent"}), encoding="utf-8")
    cr = tmp_path / "cr.json"
    cr.write_text(json.dumps({"_provider_limits": {}}), encoding="utf-8")

    host_items = {"ALA-KZT-7280TR-1": "101"}
    items = {
        ("ALA-KZT-7280TR-1", "ethernet51/1"): {
            "bits_in": "net.if.in[Eth1]",
            "bits_out": "net.if.out[Eth1]",
        },
    }

    def fake_fetch(url, token, hostnames, debug=False):
        return dict(host_items), dict(items), None

    deleted = []
    (
        ZabbixRpcMocker()
        .on("user.get", lambda p: [{"userid": "1"}])
        .on("hostgroup.get", lambda p: [{"groupid": "2"}])
        .on(
            "host.get",
            lambda p: [{"hostid": "101", "host": "Uplinks-Cogent", "name": "Uplinks Cogent"}],
        )
        .on("item.get", lambda p: [])
        .on("item.create", lambda p: {"itemids": ["1"]})
        .on("trigger.get", lambda p: [{"triggerid": "9", "description": "Provider aggregate"}])
        .on("trigger.delete", lambda p: deleted.extend(p) or True)
        .activate(monkeypatch)
    )

    with patch.object(agg, "_get_providers_from_netbox", return_value=["Cogent"]):
        with patch.object(agg, "fetch_zabbix_hosts_and_items", side_effect=fake_fetch):
            done, err = agg.run(
                "https://z.example/api_jsonrpc.php",
                "t",
                str(cr),
                str(FIXTURES / "dry_ssh_minimal.json"),
                str(desc),
                None,
                prune_triggers_without_limits=True,
            )
    assert err is None
    assert done and done[0][2] is False
    assert deleted == ["9"]

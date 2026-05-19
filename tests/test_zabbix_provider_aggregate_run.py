"""Integration test for zabbix_provider_aggregate.run() with mocked Zabbix."""

import json
from pathlib import Path
from unittest.mock import patch

import zabbix_provider_aggregate as agg
from tests.mocks.zabbix_rpc import ZabbixRpcMocker

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_run_creates_aggregate_host_and_triggers(tmp_path, monkeypatch):
    dry_ssh = FIXTURES / "dry_ssh_minimal.json"
    desc_map = tmp_path / "description_to_name.json"
    desc_map.write_text(
        json.dumps(
            {
                "Uplink: Cogent 10G": "Cogent",
                "Uplink: Hurricane": "Hurricane",
                "Uplink: Hurricane member": "Hurricane",
            }
        ),
        encoding="utf-8",
    )
    commit_rates = tmp_path / "commit_rates.json"
    commit_rates.write_text(
        json.dumps({"_provider_limits": {"Cogent": 10, "Hurricane": 5}}),
        encoding="utf-8",
    )

    host_items = {
        "ALA-KZT-7280TR-1": "101",
        "FRN-MX-1": "102",
    }
    items_by_host = {
        ("ALA-KZT-7280TR-1", "ethernet51/1"): {
            "bits_in": "net.if.in[51]",
            "bits_out": "net.if.out[51]",
        },
        ("FRN-MX-1", "ae5.0"): {
            "bits_in": "net.if.in[ae5]",
            "bits_out": "net.if.out[ae5]",
        },
    }

    def fake_fetch(url, token, hostnames, debug=False):
        h = {k: host_items[k] for k in hostnames if k in host_items}
        i = {k: v for k, v in items_by_host.items() if k[0] in h}
        return h, i, None

    item_created = []
    trigger_created = []

    mocker = (
        ZabbixRpcMocker()
        .on("user.get", lambda p: [{"userid": "1"}])
        .on("hostgroup.get", lambda p: [{"groupid": "2"}])
        .on(
            "host.get",
            lambda p: [
                {"hostid": host_items[h], "host": h, "name": h}
                for h in (p.get("filter", {}).get("host") or [])
                if h in host_items
            ],
        )
        .on("host.create", lambda p: {"hostids": ["999"]})
        .on("item.get", lambda p: [])
        .on("item.create", lambda p: item_created.append(p) or {"itemids": ["i1"]})
        .on("item.update", lambda p: True)
        .on("trigger.get", lambda p: [])
        .on("trigger.create", lambda p: trigger_created.append(p) or {"triggerids": ["t1"]})
        .on("trigger.update", lambda p: True)
    )
    mocker.activate(monkeypatch)

    with patch.object(agg, "_get_providers_from_netbox", return_value=[]):
        with patch.object(agg, "fetch_zabbix_hosts_and_items", side_effect=fake_fetch):
            done, err = agg.run(
                "https://z.example/api_jsonrpc.php",
                "token",
                str(commit_rates),
                str(dry_ssh),
                str(desc_map),
                cache_path=None,
                debug=False,
            )

    assert err is None
    assert done
    providers_done = {d[0] for d in done}
    assert "Cogent" in providers_done
    assert "Hurricane" in providers_done
    assert any("aggregate.bits.in" in (c.get("key_") or "") for c in item_created)
    assert len(trigger_created) >= 3


def test_run_prunes_triggers_without_limit(tmp_path, monkeypatch):
    dry_ssh = FIXTURES / "dry_ssh_minimal.json"
    desc_map = tmp_path / "description_to_name.json"
    desc_map.write_text(json.dumps({"Uplink: Cogent 10G": "Cogent"}), encoding="utf-8")
    commit_rates = tmp_path / "commit_rates.json"
    commit_rates.write_text(json.dumps({"_provider_limits": {}}), encoding="utf-8")

    deleted = []

    def fake_fetch(url, token, hostnames, debug=False):
        return (
            {"ALA-KZT-7280TR-1": "101"},
            {("ALA-KZT-7280TR-1", "ethernet51/1"): {"bits_in": "net.if.in[1]", "bits_out": ""}},
            None,
        )

    mocker = (
        ZabbixRpcMocker()
        .on("user.get", lambda p: [])
        .on("hostgroup.get", lambda p: [{"groupid": "2"}])
        .on("host.get", lambda p: [{"hostid": "101", "host": "ALA-KZT-7280TR-1", "name": "ALA-KZT-7280TR-1"}])
        .on("item.get", lambda p: [{"itemid": "1", "key_": "aggregate.bits.in[]"}] if "aggregate" in str(p) else [])
        .on("item.create", lambda p: {"itemids": ["1"]})
        .on("item.update", lambda p: True)
        .on(
            "trigger.get",
            lambda p: [{"triggerid": "old1", "description": "Provider aggregate traffic >= 90%"}]
            if "Provider aggregate" in str(p.get("search", {}))
            else [],
        )
        .on("trigger.delete", lambda p: deleted.extend(p) or True)
    )
    mocker.activate(monkeypatch)

    with patch.object(agg, "_get_providers_from_netbox", return_value=["Cogent"]):
        with patch.object(agg, "fetch_zabbix_hosts_and_items", side_effect=fake_fetch):
            done, err = agg.run(
                "https://z.example/api_jsonrpc.php",
                "t",
                str(commit_rates),
                str(dry_ssh),
                str(desc_map),
                cache_path=None,
                prune_triggers_without_limits=True,
            )

    assert err is None
    assert done
    assert deleted

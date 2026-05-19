"""Push zabbix_provider_services and zabbix_provider_sla to >=85%."""

import json
import sys
from unittest.mock import patch

import pytest

from tests.mocks.zabbix_rpc import ZabbixRpcMocker
import zabbix_provider_services as svc
import zabbix_provider_sla as sla


def test_parent_service_empty_name():
    assert svc._get_or_create_parent_service("https://z.example/api_jsonrpc.php", "t", "") == (None, None)


def test_parent_service_errors(monkeypatch):
    (
        ZabbixRpcMocker()
        .on("service.get", lambda p: (_ for _ in ()).throw(RuntimeError("svc get")))
        .activate(monkeypatch)
    )
    pid, err = svc._get_or_create_parent_service("https://z.example/api_jsonrpc.php", "t", "Parent")
    assert pid is None
    assert "service.get (parent)" in err

    (
        ZabbixRpcMocker()
        .on("service.get", lambda p: [])
        .on("service.create", lambda p: (_ for _ in ()).throw(RuntimeError("svc create")))
        .activate(monkeypatch)
    )
    pid, err = svc._get_or_create_parent_service("https://z.example/api_jsonrpc.php", "t", "Parent")
    assert pid is None
    assert "service.create (parent)" in err


def test_provider_service_update_error(monkeypatch):
    (
        ZabbixRpcMocker()
        .on(
            "service.get",
            lambda p: [{"serviceid": "5", "name": "Uplinks Cogent", "parents": []}],
        )
        .on("service.update", lambda p: (_ for _ in ()).throw(RuntimeError("upd")))
        .activate(monkeypatch)
    )
    sid, err = svc._get_or_create_provider_service(
        "https://z.example/api_jsonrpc.php", "t", "Cogent", None
    )
    assert sid is None
    assert "upd" in err


def test_legacy_sla_source_errors(monkeypatch):
    (
        ZabbixRpcMocker()
        .on("service.get", lambda p: (_ for _ in ()).throw(RuntimeError("leg get")))
        .activate(monkeypatch)
    )
    err_msg = svc._delete_legacy_sla_source_service(
        "https://z.example/api_jsonrpc.php", "t", "Cogent"
    )
    assert "service.get (legacy SLA source)" in err_msg

    (
        ZabbixRpcMocker()
        .on("service.get", lambda p: [{"serviceid": "9"}])
        .on("service.delete", lambda p: (_ for _ in ()).throw(RuntimeError("leg del")))
        .activate(monkeypatch)
    )
    err_msg = svc._delete_legacy_sla_source_service(
        "https://z.example/api_jsonrpc.php", "t", "Cogent"
    )
    assert "service.delete (legacy SLA source)" in err_msg


def test_burst_service_create_with_parent(monkeypatch):
    created = []
    (
        ZabbixRpcMocker()
        .on("service.get", lambda p: [])
        .on(
            "service.create",
            lambda p: created.append(p) or {"serviceids": ["11"]},
        )
        .activate(monkeypatch)
    )
    sid, err = svc._get_or_create_burst_circuit_service(
        "https://z.example/api_jsonrpc.php", "t", "Cogent", "CKT-1", parentid="3"
    )
    assert err is None
    assert sid == "11"
    payload = created[0][0] if isinstance(created[0], list) else created[0]
    assert payload["parents"] == [{"serviceid": "3"}]


def test_ensure_provider_sla_create(monkeypatch):
    (
        ZabbixRpcMocker()
        .on("sla.get", lambda p: [])
        .on("sla.create", lambda p: {"slaids": ["8"]})
        .activate(monkeypatch)
    )
    slaid, err = svc._ensure_provider_sla(
        "https://z.example/api_jsonrpc.php", "t", "Cogent", 99.9
    )
    assert err is None
    assert slaid == "8"


def test_main_provider_errors_continue(tmp_path, monkeypatch, capsys, zabbix_env):
    cr = tmp_path / "cr.json"
    cr.write_text(
        json.dumps({"_provider_limits": {"Bad": 10}, "_provider_sla": 99.0}),
        encoding="utf-8",
    )

    def service_get(params):
        name = (params.get("filter") or {}).get("name", [""])[0]
        if "Bad" in name:
            return []
        return []

    (
        ZabbixRpcMocker()
        .on("user.get", lambda p: [{"userid": "1"}])
        .on("service.get", service_get)
        .on("service.create", lambda p: (_ for _ in ()).throw(RuntimeError("create fail")))
        .activate(monkeypatch)
    )
    monkeypatch.setattr(sys, "argv", ["zabbix_provider_services.py", "-f", str(cr)])
    svc.main()
    err = capsys.readouterr().err
    assert "create fail" in err or "Provider Bad" in err


def test_compute_sla_from_events():
    events = [(100, 1), (200, 0), (300, 1)]
    total, problem = sla._compute_sla_from_events(events, 0, 400)
    assert total == 400
    assert problem == 200


def test_load_events_skips_bad_values(monkeypatch):
    (
        ZabbixRpcMocker()
        .on(
            "event.get",
            lambda p: [
                {"clock": "bad", "value": 1},
                {"clock": 150, "value": "x"},
                {"clock": 200, "value": 0},
            ],
        )
        .activate(monkeypatch)
    )
    events = sla._load_events_for_trigger("https://z.example/api_jsonrpc.php", "t", "1", 0, 300)
    assert events == [(150, 0), (200, 0)]


def test_get_aggregate_triggers_with_hosts(monkeypatch):
    agg_host = "Uplinks Cogent"
    (
        ZabbixRpcMocker()
        .on(
            "host.get",
            lambda p: [{"hostid": "200", "host": "Cogent-San", "name": agg_host}],
        )
        .on(
            "trigger.get",
            lambda p: [
                {
                    "triggerid": "1",
                    "description": "Provider aggregate traffic >= 90% of limit (10 Gbps)",
                    "hosts": [{"hostid": "200"}],
                },
                {
                    "triggerid": "2",
                    "description": "Provider aggregate traffic >= 100% of limit (10 Gbps)",
                    "hosts": [{"hostid": "200"}],
                },
                {
                    "triggerid": "3",
                    "description": "Provider aggregate SLA breach: x",
                    "hosts": [{"hostid": "200"}],
                },
            ],
        )
        .activate(monkeypatch)
    )
    out = sla._get_aggregate_triggers("https://z.example/api_jsonrpc.php", "t", ["Cogent"])
    assert out["Cogent"][0] == "1"
    assert out["Cogent"][1] == "2"
    assert out["Cogent"][2] == "3"


def test_main_with_events_and_from_ts(tmp_path, monkeypatch, zabbix_env, capsys):
    cr = tmp_path / "cr.json"
    cr.write_text(
        json.dumps({"_provider_limits": {"Cogent": 10}, "_provider_sla": 99.0}),
        encoding="utf-8",
    )
    (
        ZabbixRpcMocker()
        .on("host.get", lambda p: [{"hostid": "200", "host": "Uplinks Cogent", "name": "Uplinks Cogent"}])
        .on(
            "trigger.get",
            lambda p: [
                {
                    "triggerid": "99",
                    "description": "Provider aggregate traffic >= 100% of limit (10 Gbps)",
                    "hosts": [{"hostid": "200"}],
                },
            ],
        )
        .on(
            "event.get",
            lambda p: [{"clock": 100, "value": 1}, {"clock": 200, "value": 0}],
        )
        .activate(monkeypatch)
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "zabbix_provider_sla.py",
            "-f",
            str(cr),
            "--from-ts",
            "0",
            "--to-ts",
            "1000",
        ],
    )
    sla.main()
    out = capsys.readouterr().out
    assert "Cogent" in out
    assert "BelowSLA" in out

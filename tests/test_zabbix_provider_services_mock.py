"""zabbix_provider_services.py with mocked Zabbix API."""

import json
from unittest.mock import patch

import zabbix_provider_services as svc
from tests.mocks.zabbix_rpc import ZabbixRpcMocker


def _service_handlers():
    stores = {"services": [], "slas": []}

    def service_get(params):
        name = (params.get("filter") or {}).get("name", [""])[0]
        for s in stores["services"]:
            if s.get("name") == name:
                return [s]
        return []

    def service_create(params):
        if isinstance(params, list):
            params = params[0]
        sid = str(len(stores["services"]) + 1)
        rec = {"serviceid": sid, "name": params["name"], "parents": []}
        stores["services"].append(rec)
        return {"serviceids": [sid]}

    def service_update(params):
        return True

    def service_delete(params):
        stores["services"] = [s for s in stores["services"] if s["serviceid"] not in params]
        return True

    def sla_get(params):
        name = (params.get("filter") or {}).get("name", [""])[0]
        for s in stores["slas"]:
            if s.get("name") == name:
                return [s]
        return []

    def sla_create(params):
        payload = params[0] if isinstance(params, list) else params
        sid = str(len(stores["slas"]) + 1)
        stores["slas"].append({"slaid": sid, "name": payload["name"]})
        return {"slaids": [sid]}

    def sla_update(params):
        return True

    return stores, service_get, service_create, service_update, service_delete, sla_get, sla_create, sla_update


def test_get_or_create_parent_and_provider_service(monkeypatch):
    stores, service_get, service_create, service_update, service_delete, sla_get, sla_create, sla_update = _service_handlers()
    mocker = (
        ZabbixRpcMocker()
        .on("service.get", service_get)
        .on("service.create", service_create)
        .on("service.update", service_update)
        .on("service.delete", service_delete)
    )
    mocker.activate(monkeypatch)

    pid, err = svc._get_or_create_parent_service("https://z.example/api_jsonrpc.php", "t", "Uplinks providers")
    assert err is None
    assert pid == "1"

    sid, err = svc._get_or_create_provider_service("https://z.example/api_jsonrpc.php", "t", "Cogent", pid)
    assert err is None
    assert sid == "2"

    sid2, err = svc._get_or_create_provider_service("https://z.example/api_jsonrpc.php", "t", "Cogent", pid)
    assert err is None
    assert sid2 == "2"


def test_burst_circuit_service_and_sla(monkeypatch):
    stores, service_get, service_create, service_update, service_delete, sla_get, sla_create, sla_update = _service_handlers()
    (
        ZabbixRpcMocker()
        .on("service.get", service_get)
        .on("service.create", service_create)
        .on("service.update", service_update)
        .on("sla.get", sla_get)
        .on("sla.create", sla_create)
        .on("sla.update", sla_update)
        .activate(monkeypatch)
    )

    sid, err = svc._get_or_create_burst_circuit_service(
        "https://z.example/api_jsonrpc.php", "t", "Cogent", "Cogent-ALA-1", None
    )
    assert err is None
    assert sid == "1"

    slaid, err = svc._ensure_burst_circuit_sla("https://z.example/api_jsonrpc.php", "t", "Cogent-ALA-1", 99.95)
    assert err is None
    assert slaid == "1"

    slaid2, err = svc._ensure_provider_sla("https://z.example/api_jsonrpc.php", "t", "Cogent", 99.95)
    assert err is None
    assert slaid2 == "2"


def test_delete_legacy_sla_source(monkeypatch):
    (
        ZabbixRpcMocker()
        .on("service.get", lambda p: [{"serviceid": "99"}] if "SLA source" in str(p) else [])
        .on("service.delete", lambda p: True)
        .activate(monkeypatch)
    )
    err = svc._delete_legacy_sla_source_service("https://z.example/api_jsonrpc.php", "t", "Cogent")
    assert err is None


def test_main_creates_services(tmp_path, monkeypatch):
    cr = tmp_path / "commit_rates.json"
    cr.write_text(
        json.dumps(
            {
                "_provider_limits": {"Cogent": 10},
                "_provider_sla": 99.95,
                "h1": {
                    "Eth1": {
                        "billing_model": "Burst",
                        "provider": "Cogent",
                        "circuit_id": "Cogent-ALA-1",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    stores, service_get, service_create, service_update, service_delete, sla_get, sla_create, sla_update = _service_handlers()
    (
        ZabbixRpcMocker()
        .on("user.get", lambda p: [])
        .on("service.get", service_get)
        .on("service.create", service_create)
        .on("service.update", service_update)
        .on("service.delete", service_delete)
        .on("sla.get", sla_get)
        .on("sla.create", sla_create)
        .on("sla.update", sla_update)
        .activate(monkeypatch)
    )

    monkeypatch.setenv("ZABBIX_URL", "https://zabbix.example")
    monkeypatch.setenv("ZABBIX_TOKEN", "token")

    with patch.object(svc.sys, "argv", ["zabbix_provider_services.py", "-f", str(cr)]):
        svc.main()

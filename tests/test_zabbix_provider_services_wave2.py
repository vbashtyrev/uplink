"""zabbix_provider_services: burst SLA update, parent service, circuit service update."""

import json

from tests.mocks.zabbix_rpc import ZabbixRpcMocker
import zabbix_provider_services as svc


def test_get_or_create_burst_circuit_service_update(monkeypatch):
    stores = {"services": [{"serviceid": "10", "name": "Uplinks Burst CKT-1", "parents": []}]}

    def service_get(params):
        name = (params.get("filter") or {}).get("name", [""])[0]
        return [s for s in stores["services"] if s["name"] == name]

    (
        ZabbixRpcMocker()
        .on("service.get", service_get)
        .on("service.update", lambda p: True)
        .activate(monkeypatch)
    )
    sid, err = svc._get_or_create_burst_circuit_service(
        "https://z.example/api_jsonrpc.php",
        "t",
        "Cogent",
        "CKT-1",
        parentid="5",
    )
    assert err is None
    assert sid == "10"


def test_ensure_burst_circuit_sla_update(monkeypatch):
    (
        ZabbixRpcMocker()
        .on(
            "sla.get",
            lambda p: [{"slaid": "3", "name": "Uplinks Burst CKT-1 SLA", "slo": 99.0}],
        )
        .on("sla.update", lambda p: True)
        .activate(monkeypatch)
    )
    slaid, err = svc._ensure_burst_circuit_sla(
        "https://z.example/api_jsonrpc.php", "t", "CKT-1", 99.95
    )
    assert err is None
    assert slaid == "3"


def test_main_with_parent_service(tmp_path, monkeypatch, capsys):
    cr = tmp_path / "commit_rates.json"
    cr.write_text(
        json.dumps(
            {
                "_provider_limits": {"Cogent": 10},
                "_provider_sla": 99.9,
                "h1": {
                    "Eth1": {
                        "billing_model": "Burst",
                        "provider": "Cogent",
                        "circuit_id": "CKT-1",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    stores = {"services": [], "slas": []}

    def service_get(params):
        name = (params.get("filter") or {}).get("name", [""])[0]
        return [s for s in stores["services"] if s["name"] == name]

    def service_create(params):
        payload = params[0] if isinstance(params, list) else params
        sid = str(len(stores["services"]) + 1)
        rec = {"serviceid": sid, "name": payload["name"], "parents": payload.get("parents", [])}
        stores["services"].append(rec)
        return {"serviceids": [sid]}

    (
        ZabbixRpcMocker()
        .on("user.get", lambda p: [])
        .on("service.get", service_get)
        .on("service.create", service_create)
        .on("service.update", lambda p: True)
        .on("service.delete", lambda p: True)
        .on("sla.get", lambda p: [])
        .on("sla.create", lambda p: {"slaids": ["1"]})
        .on("sla.update", lambda p: True)
        .activate(monkeypatch)
    )
    monkeypatch.setenv("ZABBIX_URL", "https://z.example/api_jsonrpc.php")
    monkeypatch.setenv("ZABBIX_TOKEN", "t")
    import sys

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "zabbix_provider_services.py",
            "-f",
            str(cr),
            "--parent-service",
            "Uplinks root",
        ],
    )
    svc.main()
    names = {s["name"] for s in stores["services"]}
    assert "Uplinks root" in names or any("Cogent" in n for n in names)

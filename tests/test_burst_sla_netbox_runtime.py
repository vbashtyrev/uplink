"""Runtime Burst/SLA transition: NetBox inventory first, commit_rates.json fallback."""

import json
import sys
from unittest.mock import patch

import pytest

from tests.mocks.netbox_api import build_netbox_for_commit_rates
from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from uplinks.netbox.inventory import (
    burst_circuits_unique_from_inventory,
    burst_metadata_from_inventory,
    burst_pairs_from_inventory,
    collect_provider_slo_percent,
    collect_uplink_inventory,
)
import zabbix_provider_services as svc
from zabbix_sync_commit_rate import load_burst_metadata, load_burst_pairs


def _choice_record(value, label=None):
    return {"value": value, "label": label or value}


def test_burst_pairs_from_inventory_billing_model_custom_field():
    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
        provider_name="ManualISP",
        circuit_custom_fields={"billing_model": _choice_record("Burst", "Burst billing")},
        tag_device=False,
    )
    with patch("uplinks.netbox.inventory.netbox_border_tag", return_value=None):
        report = collect_uplink_inventory(nb, tag=None)
    pairs = burst_pairs_from_inventory(report)
    assert pairs == {("ALA-KZT-7280TR-1", "Ethernet51/1")}


def test_burst_metadata_from_inventory_manual_provider():
    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
        provider_name="ManualISP",
        circuit_id=42,
        circuit_custom_fields={"billing_model": "burst"},
        tag_device=False,
    )
    with patch("uplinks.netbox.inventory.netbox_border_tag", return_value=None):
        report = collect_uplink_inventory(nb, tag=None)
    meta = burst_metadata_from_inventory(report)
    assert meta[("ALA-KZT-7280TR-1", "Ethernet51/1")] == {
        "provider": "ManualISP",
        "circuit_id": "CKT-42",
    }


def test_load_burst_pairs_prefers_inventory(tmp_path):
    nb = build_netbox_for_commit_rates(
        device_name="R1",
        iface_name="Eth1",
        circuit_custom_fields={"billing_model": "Burst"},
        tag_device=False,
    )
    with patch("uplinks.netbox.inventory.netbox_border_tag", return_value=None):
        report = collect_uplink_inventory(nb, tag=None)
    cr = tmp_path / "commit_rates.json"
    cr.write_text(
        json.dumps({"Other": {"Eth9": {"billing_model": "Burst"}}}),
        encoding="utf-8",
    )
    pairs = load_burst_pairs(str(cr), inventory_report=report, debug=True)
    assert pairs == {("R1", "Eth1"), ("Other", "Eth9")}


def test_load_burst_pairs_partial_fallback_warning(tmp_path, capsys):
    nb = build_netbox_for_commit_rates(tag_device=False)
    with patch("uplinks.netbox.inventory.netbox_border_tag", return_value=None):
        report = collect_uplink_inventory(nb, tag=None)
    cr = tmp_path / "commit_rates.json"
    cr.write_text(
        json.dumps({"H": {"Eth1": {"billing_model": "Burst"}}}),
        encoding="utf-8",
    )
    pairs = load_burst_pairs(str(cr), inventory_report=report, debug=False)
    assert pairs == {("H", "Eth1")}
    err = capsys.readouterr().err
    assert "transition fallback" in err


def test_load_burst_metadata_prefers_inventory(tmp_path):
    nb = build_netbox_for_commit_rates(
        device_name="R1",
        iface_name="Eth1",
        provider_name="Cogent",
        circuit_id=7,
        circuit_custom_fields={"billing_model": "Burst"},
        tag_device=False,
    )
    with patch("uplinks.netbox.inventory.netbox_border_tag", return_value=None):
        report = collect_uplink_inventory(nb, tag=None)
    cr = tmp_path / "commit_rates.json"
    cr.write_text(
        json.dumps(
            {
                "R1": {
                    "Eth1": {
                        "billing_model": "Burst",
                        "provider": "Wrong",
                        "circuit_id": "WRONG",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    meta = load_burst_metadata(str(cr), inventory_report=report)
    assert meta[("R1", "Eth1")] == {"provider": "Cogent", "circuit_id": "CKT-7"}
    assert ("Other", "Eth9") not in meta


def test_load_burst_metadata_partial_fallback(tmp_path, capsys):
    nb = build_netbox_for_commit_rates(
        device_name="R1",
        iface_name="Eth1",
        provider_name="Cogent",
        circuit_id=7,
        circuit_custom_fields={"billing_model": "Burst"},
        tag_device=False,
    )
    with patch("uplinks.netbox.inventory.netbox_border_tag", return_value=None):
        report = collect_uplink_inventory(nb, tag=None)
    cr = tmp_path / "commit_rates.json"
    cr.write_text(
        json.dumps(
            {
                "R1": {
                    "Eth1": {
                        "billing_model": "Burst",
                        "provider": "Wrong",
                        "circuit_id": "WRONG",
                    },
                },
                "Other": {
                    "Eth9": {
                        "billing_model": "Burst",
                        "provider": "LegacyISP",
                        "circuit_id": "CKT-JSON",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    meta = load_burst_metadata(str(cr), inventory_report=report)
    assert meta[("R1", "Eth1")] == {"provider": "Cogent", "circuit_id": "CKT-7"}
    assert meta[("Other", "Eth9")] == {"provider": "LegacyISP", "circuit_id": "CKT-JSON"}
    err = capsys.readouterr().err
    assert "Eth9" in err or "Other" in err
    assert "transition fallback" in err


def test_load_burst_pairs_invalid_json_with_inventory_fails(tmp_path):
    nb = build_netbox_for_commit_rates(tag_device=False)
    with patch("uplinks.netbox.inventory.netbox_border_tag", return_value=None):
        report = collect_uplink_inventory(nb, tag=None)
    cr = tmp_path / "commit_rates.json"
    cr.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        load_burst_pairs(str(cr), inventory_report=report)


def test_burst_circuits_unique_from_inventory():
    report = {
        "complete": [
            {
                "billing_model": "Burst",
                "provider": "P",
                "circuit_id": "P-1",
                "device": "h1",
                "interface": "e1",
            },
            {
                "billing_model": "burst",
                "provider": "P",
                "circuit_id": "P-1",
                "device": "h2",
                "interface": "e3",
            },
            {
                "billing_model": "Flat",
                "provider": "P",
                "circuit_id": "P-2",
                "device": "h1",
                "interface": "e2",
            },
        ],
    }
    assert burst_circuits_unique_from_inventory(report) == [("P-1", "P")]


def test_collect_provider_slo_percent():
    from tests.mocks.netbox_api import _Providers, _Record

    nb = type("NB", (), {})()
    nb.circuits = type("C", (), {})()
    nb.circuits.providers = _Providers(
        [
            _Record(name="Cogent", custom_fields={"slo_percent": 99.95}),
            _Record(name="ManualISP", custom_fields={}),
        ]
    )
    slo, err = collect_provider_slo_percent(nb, debug=False)
    assert slo == {"Cogent": 99.95}
    assert err is None


def test_collect_provider_slo_percent_auth_denied():
    nb = type("NB", (), {})()
    nb.circuits = type("C", (), {})()
    nb.circuits.providers = type(
        "P",
        (),
        {"all": lambda self: (_ for _ in ()).throw(Exception("403 Forbidden"))},
    )()
    slo, err = collect_provider_slo_percent(nb, debug=False)
    assert slo == {}
    assert err == svc.ERROR_AUTH_DENIED


def test_load_netbox_services_context_slo_auth_denied(monkeypatch):
    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
        provider_name="ManualISP",
        tag_device=False,
    )

    def fake_slo(_nb, debug=False):
        return {}, svc.ERROR_AUTH_DENIED

    with patch("zabbix_provider_services.netbox_client_from_env", return_value=nb), patch(
        "zabbix_provider_services.netbox_border_tag", return_value=None
    ), patch("zabbix_provider_services.collect_provider_slo_percent", fake_slo):
        ctx = svc._load_netbox_services_context(debug=False)
    assert ctx == {"auth_denied": True}


def test_resolve_providers_netbox_manual_without_tag():
    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
        provider_name="ManualISP",
        tag_device=False,
    )
    with patch("zabbix_provider_services.netbox_client_from_env", return_value=nb), patch(
        "zabbix_provider_services.netbox_border_tag", return_value=None
    ):
        ctx = svc._load_netbox_services_context(debug=False)
    assert ctx is not None
    providers = svc._resolve_providers(ctx, {}, debug=False)
    assert providers == ["ManualISP"]


def test_resolve_providers_fallback_commit_rates(capsys):
    commit_rates = {"_provider_limits": {"LegacyISP": 10}}
    providers = svc._resolve_providers(None, commit_rates, debug=True)
    assert providers == ["LegacyISP"]
    assert "transition fallback" in capsys.readouterr().err


def test_resolve_providers_partial_fallback_from_commit_rates(capsys):
    ctx = {"providers": {"ManualISP"}}
    commit_rates = {"_provider_limits": {"ManualISP": 5, "LegacyISP": 10}}
    providers = svc._resolve_providers(ctx, commit_rates, debug=False)
    assert providers == ["LegacyISP", "ManualISP"]
    err = capsys.readouterr().err
    assert "LegacyISP" in err
    assert "transition fallback" in err


def test_resolve_burst_circuits_partial_fallback(capsys):
    ctx = {"burst_circuits": [("CKT-NB", "ManualISP")]}
    commit_rates = {
        "h1": {
            "Eth1": {
                "billing_model": "Burst",
                "provider": "LegacyISP",
                "circuit_id": "CKT-JSON",
            },
        },
    }
    burst_pairs = svc._resolve_burst_circuits(ctx, commit_rates, debug=False)
    assert burst_pairs == [("CKT-JSON", "LegacyISP"), ("CKT-NB", "ManualISP")]
    err = capsys.readouterr().err
    assert "CKT-JSON" in err
    assert "transition fallback" in err


def test_load_netbox_services_context_auth_denied(monkeypatch):
    nb = type("NB", (), {})()

    def fake_collect(_nb, **kwargs):
        return {"stats": {"error": svc.ERROR_AUTH_DENIED}, "complete": [], "incomplete": []}

    with patch("zabbix_provider_services.netbox_client_from_env", return_value=nb), patch(
        "zabbix_provider_services.collect_uplink_inventory", fake_collect
    ):
        ctx = svc._load_netbox_services_context(debug=False)
    assert ctx == {"auth_denied": True}


def test_main_auth_denied_exits_without_commit_rates_fallback(tmp_path, monkeypatch, capsys, zabbix_env):
    missing_cr = tmp_path / "missing-commit-rates.json"
    (
        ZabbixRpcMocker()
        .on("user.get", lambda p: [{"userid": "1"}])
        .activate(monkeypatch)
    )

    def fake_load_ctx(debug=False):
        return {"auth_denied": True}

    monkeypatch.setattr("zabbix_provider_services._load_netbox_services_context", fake_load_ctx)
    monkeypatch.setattr(sys, "argv", ["zabbix_provider_services.py", "-f", str(missing_cr)])
    with pytest.raises(SystemExit) as exc:
        svc.main()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "NetBox error" in err
    assert "transition fallback" not in err


def test_main_netbox_without_commit_rates_file(tmp_path, monkeypatch):
    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
        provider_name="ManualISP",
        provider_custom_fields={"slo_percent": 99.88},
        circuit_id=55,
        circuit_custom_fields={"billing_model": "Burst"},
        tag_device=False,
    )
    missing_cr = tmp_path / "missing-commit-rates.json"

    stores, service_get, service_create, service_update, service_delete, sla_get, sla_create, sla_update = _service_handlers()
    (
        ZabbixRpcMocker()
        .on("user.get", lambda p: [{"userid": "1"}])
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
    with patch("zabbix_provider_services.netbox_client_from_env", return_value=nb), patch(
        "zabbix_provider_services.netbox_border_tag", return_value=None
    ):
        monkeypatch.setattr(
            sys,
            "argv",
            ["zabbix_provider_services.py", "-f", str(missing_cr), "--parent-service", "Uplinks providers"],
        )
        svc.main()

    service_names = [s["name"] for s in stores["services"]]
    assert "Uplinks ManualISP" in service_names
    assert "Uplinks Burst CKT-55" in service_names


def test_resolve_provider_slo_netbox_then_fallback(capsys):
    ctx = {
        "providers": {"Cogent", "ManualISP"},
        "provider_slo_percent": {"Cogent": 99.9},
    }
    assert svc._resolve_provider_slo("Cogent", ctx, 99.5) == 99.9
    assert svc._resolve_provider_slo("ManualISP", ctx, 99.5) == 99.5
    err = capsys.readouterr().err
    assert "slo_percent" in err
    assert "transition fallback" in err


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
        stores["slas"].append({"slaid": sid, "name": payload["name"], "slo": payload["slo"]})
        return {"slaids": [sid]}

    def sla_update(params):
        return True

    return stores, service_get, service_create, service_update, service_delete, sla_get, sla_create, sla_update


def test_main_netbox_burst_and_slo(tmp_path, monkeypatch):
    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
        provider_name="ManualISP",
        provider_custom_fields={"slo_percent": 99.88, "aggregate_limit_gbps": 5},
        circuit_id=55,
        circuit_custom_fields={"billing_model": "Burst"},
        tag_device=False,
    )
    cr = tmp_path / "commit_rates.json"
    cr.write_text(
        json.dumps(
            {
                "_provider_limits": {"Wrong": 1},
                "_provider_sla": 99.0,
                "ignored": {
                    "Eth1": {
                        "billing_model": "Burst",
                        "provider": "Wrong",
                        "circuit_id": "WRONG",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    stores, service_get, service_create, service_update, service_delete, sla_get, sla_create, sla_update = _service_handlers()
    (
        ZabbixRpcMocker()
        .on("user.get", lambda p: [{"userid": "1"}])
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
    with patch("zabbix_provider_services.netbox_client_from_env", return_value=nb), patch(
        "zabbix_provider_services.netbox_border_tag", return_value=None
    ):
        monkeypatch.setattr(
            sys,
            "argv",
            ["zabbix_provider_services.py", "-f", str(cr), "--parent-service", "Uplinks providers"],
        )
        svc.main()

    service_names = [s["name"] for s in stores["services"]]
    assert "Uplinks ManualISP" in service_names
    assert "Uplinks Burst CKT-55" in service_names
    sla_values = {s["name"]: s["slo"] for s in stores["slas"]}
    assert sla_values["Uplinks ManualISP SLA"] == 99.88
    assert sla_values["Uplinks Burst CKT-55 SLA"] == 99.88

"""zabbix_provider_services error paths and service.update with parent."""

import json
import sys
from pathlib import Path

import pytest

from tests.mocks.zabbix_rpc import ZabbixRpcMocker
import zabbix_provider_services as svc


def test_load_commit_rates_errors(tmp_path):
    missing, err = svc._load_commit_rates(str(tmp_path / "nope.json"))
    assert missing is None
    assert "not found" in err

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    missing, err = svc._load_commit_rates(str(bad))
    assert missing is None
    assert "invalid JSON" in err

    root = tmp_path / "list.json"
    root.write_text("[]", encoding="utf-8")
    missing, err = svc._load_commit_rates(str(root))
    assert missing is None
    assert "unexpected JSON" in err


def test_get_providers_from_limits_invalid():
    assert svc._get_providers_from_limits({"_provider_limits": "x"}) == []
    assert svc._get_providers_from_limits({"_provider_limits": {"": 1, "Cogent": 10}}) == ["Cogent"]


def test_iter_burst_links_skips_invalid():
    data = {
        "_skip": {},
        123: {"Eth1": {}},
        "dev": {
            "Eth1": "not-dict",
            "Eth2": {"billing_model": "burst", "provider": "Cogent", "circuit_id": "CKT-1"},
        },
        "dev2": {
            "Eth3": {"billing_model": "Burst", "provider": "Cogent", "circuit_id": ""},
            "Eth4": {"billing_model": "Burst", "provider": "", "circuit_id": "C1"},
        },
    }
    assert list(svc._iter_burst_links(data)) == [("dev", "Eth2", "Cogent", "CKT-1")]


def test_get_global_provider_sla_invalid():
    assert svc._get_global_provider_sla({"_provider_sla": "bad"}) is None
    assert svc._get_global_provider_sla({"_provider_sla": 99.9}) == 99.9


def test_get_or_create_provider_service_update_parent(monkeypatch):
    stores = {
        "services": [
            {
                "serviceid": "5",
                "name": "Uplinks Cogent",
                "parents": [],
            },
        ],
    }

    def service_get(params):
        name = (params.get("filter") or {}).get("name", [""])[0]
        return [s for s in stores["services"] if s["name"] == name]

    updates = []

    def service_update(params):
        updates.append(params)
        return True

    (
        ZabbixRpcMocker()
        .on("service.get", service_get)
        .on("service.update", service_update)
        .activate(monkeypatch)
    )
    sid, err = svc._get_or_create_provider_service(
        "https://z.example/api_jsonrpc.php", "t", "Cogent", parentid="1"
    )
    assert err is None
    assert sid == "5"
    assert updates[0].get("parents") == [{"serviceid": "1"}]


def test_main_missing_zabbix_env(monkeypatch, tmp_path):
    cr = tmp_path / "cr.json"
    cr.write_text(json.dumps({"_provider_limits": {"Cogent": 10}}), encoding="utf-8")
    monkeypatch.delenv("ZABBIX_URL", raising=False)
    monkeypatch.delenv("ZABBIX_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", ["zabbix_provider_services.py", "-f", str(cr)])
    with pytest.raises(SystemExit):
        svc.main()


def test_main_invalid_token(monkeypatch, tmp_path, zabbix_env):
    cr = tmp_path / "cr.json"
    cr.write_text(json.dumps({"_provider_limits": {"Cogent": 10}}), encoding="utf-8")
    monkeypatch.setattr("zabbix_provider_services.validate_zabbix_token", lambda *a, **k: (False, "bad"))
    monkeypatch.setattr(sys, "argv", ["zabbix_provider_services.py", "-f", str(cr)])
    with pytest.raises(SystemExit):
        svc.main()


def test_main_nothing_to_do(tmp_path, monkeypatch, capsys, zabbix_env):
    cr = tmp_path / "empty.json"
    cr.write_text("{}", encoding="utf-8")
    (
        ZabbixRpcMocker()
        .on("user.get", lambda p: [{"userid": "1"}])
        .activate(monkeypatch)
    )
    monkeypatch.setattr(sys, "argv", ["zabbix_provider_services.py", "-f", str(cr)])
    with pytest.raises(SystemExit):
        svc.main()
    assert "nothing to do" in capsys.readouterr().err

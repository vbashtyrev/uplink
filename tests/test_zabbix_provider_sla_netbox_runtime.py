"""Runtime SLA report: NetBox inventory authoritative; legacy commit_rates opt-in."""

import json
import sys
from unittest.mock import patch

import pytest

from tests.mocks.netbox_api import build_netbox_for_commit_rates
from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from tests.test_zabbix_sync_extended import MSK_MX204_1_DRY_SSH, MSK_MX204_2_DRY_SSH
from uplinks.netbox.inventory import collect_uplink_inventory
from uplinks_config import TRIGGER_DESC_SLA_BREACH_SUFFIX
import uplinks_config
import zabbix_provider_sla as sla_mod


def _inventory_report_for_circuit(nb):
    with patch("uplinks.netbox.inventory.netbox_border_tag", return_value=None):
        return collect_uplink_inventory(nb, tag=None)


def _zabbix_burst_mocker(device_name, iface_name, circuit_id):
    iface_prefix = "Interface {}:".format(iface_name)
    sla_desc = iface_prefix + " " + TRIGGER_DESC_SLA_BREACH_SUFFIX

    def host_get(params):
        filt = params.get("filter") or {}
        if "host" in filt:
            return [{"hostid": "101", "host": device_name}]
        return []

    def trigger_get(params):
        search = (params.get("search") or {}).get("description") or ""
        if "hostids" in params and search == iface_prefix:
            return [
                {
                    "triggerid": "sla1",
                    "description": sla_desc,
                },
            ]
        return []

    return (
        ZabbixRpcMocker()
        .on("host.get", host_get)
        .on("trigger.get", trigger_get)
        .on("event.get", lambda p: [{"clock": "1000", "value": "1"}, {"clock": "2000", "value": "0"}])
    )


def _burst_row_for_circuit(output, circuit_id):
    for line in output.splitlines():
        if circuit_id in line and "Burst circuits" not in line and "Circuit" not in line:
            return line
    return ""


def test_main_netbox_without_commit_rates_file(tmp_path, monkeypatch, capsys, zabbix_env):
    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
        provider_name="ManualISP",
        provider_custom_fields={"aggregate_limit_gbps": 5},
        circuit_id=55,
        circuit_custom_fields={"billing_model": "Burst"},
        commit_rate_kbps=10_000_000,
        tag_device=False,
    )
    missing_cr = tmp_path / "missing-commit-rates.json"
    agg = sla_mod.UPLINKS_AGGREGATE_HOST_PREFIX + "ManualISP"

    def host_get(params):
        filt = params.get("filter") or {}
        names = (filt.get("host") or []) + (filt.get("name") or [])
        if agg in names:
            return [{"hostid": "50", "host": agg, "name": agg}]
        if "ALA-KZT-7280TR-1" in names:
            return [{"hostid": "101", "host": "ALA-KZT-7280TR-1"}]
        return []

    def trigger_get(params):
        if "hostids" in params and params.get("hostids") == ["101"]:
            return [
                {
                    "triggerid": "burst-sla",
                    "description": "Interface Ethernet51/1: SLA breach",
                    "hosts": [{"hostid": "101"}],
                },
            ]
        if "hostids" in params:
            return [
                {
                    "triggerid": "agg-sla",
                    "description": "Provider aggregate SLA breach: ManualISP",
                    "hosts": [{"hostid": "50"}],
                },
            ]
        return []

    (
        ZabbixRpcMocker()
        .on("host.get", host_get)
        .on("trigger.get", trigger_get)
        .on("event.get", lambda p: [])
        .activate(monkeypatch)
    )

    with patch("zabbix_provider_services.netbox_client_from_env", return_value=nb), patch(
        "zabbix_provider_services.netbox_border_tag", return_value=None
    ):
        monkeypatch.setattr(
            sys,
            "argv",
            ["zabbix_provider_sla.py", "-f", str(missing_cr), "--days", "1"],
        )
        sla_mod.main()

    out = capsys.readouterr().out
    assert "SLA window" in out
    assert "ManualISP" in out
    assert "CKT-55" in out
    assert "Burst circuits" in out


def test_main_invalid_commit_rates_without_legacy_uses_netbox_only(
    tmp_path, monkeypatch, capsys, zabbix_env
):
    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
        provider_name="ManualISP",
        provider_custom_fields={"slo_percent": 99.88},
        tag_device=False,
    )
    bad_cr = tmp_path / "commit_rates.json"
    bad_cr.write_text("{not json", encoding="utf-8")

    agg = sla_mod.UPLINKS_AGGREGATE_HOST_PREFIX + "ManualISP"
    (
        ZabbixRpcMocker()
        .on(
            "host.get",
            lambda p: [{"hostid": "50", "host": agg, "name": agg}],
        )
        .on(
            "trigger.get",
            lambda p: [
                {
                    "triggerid": "agg-sla",
                    "description": "Provider aggregate SLA breach: ManualISP",
                    "hosts": [{"hostid": "50"}],
                },
            ],
        )
        .on("event.get", lambda p: [])
        .activate(monkeypatch)
    )

    with patch("zabbix_provider_services.netbox_client_from_env", return_value=nb), patch(
        "zabbix_provider_services.netbox_border_tag", return_value=None
    ):
        monkeypatch.setattr(
            sys,
            "argv",
            ["zabbix_provider_sla.py", "-f", str(bad_cr), "--days", "1"],
        )
        sla_mod.main()

    captured = capsys.readouterr()
    assert "invalid JSON" not in captured.err
    assert "legacy fallback" not in captured.err
    assert "ManualISP" in captured.out


def test_main_legacy_flag_rejects_invalid_commit_rates(tmp_path, monkeypatch, capsys, zabbix_env):
    bad_cr = tmp_path / "commit_rates.json"
    bad_cr.write_text("{not json", encoding="utf-8")

    monkeypatch.setattr("zabbix_provider_services.netbox_client_from_env", lambda **k: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "zabbix_provider_sla.py",
            "-f",
            str(bad_cr),
            "--legacy-commit-rates-fallback",
            "--days",
            "1",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        sla_mod.main()
    assert exc.value.code == 1
    assert "invalid JSON" in capsys.readouterr().err


def test_beeline_burst_sla_report_circuit_41(tmp_path, monkeypatch, capsys, zabbix_env):
    iface_name = "ae5.0"
    nb = build_netbox_for_commit_rates(
        device_name="MSK-M9-MX204-1",
        iface_name="et-0/0/3",
        provider_name="Beeline",
        circuit_id=41,
        circuit_custom_fields={"billing_model": "Burst"},
        commit_rate_kbps=10_000_000,
        tag_device=False,
    )
    dry_ssh = tmp_path / "dry-ssh.json"
    dry_ssh.write_text(json.dumps({"devices": MSK_MX204_1_DRY_SSH}), encoding="utf-8")
    _zabbix_burst_mocker("MSK-M9-MX204-1", iface_name, "CKT-41").activate(monkeypatch)

    with patch("zabbix_provider_services.netbox_client_from_env", return_value=nb), patch(
        "zabbix_provider_services.netbox_border_tag", return_value=None
    ):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "zabbix_provider_sla.py",
                "-f",
                str(tmp_path / "ignored.json"),
                "--days",
                "1",
                "--from-ts",
                "0",
                "--to-ts",
                "10000",
                "--dry-ssh",
                str(dry_ssh),
            ],
        )
        sla_mod.main()

    out = capsys.readouterr().out
    row = _burst_row_for_circuit(out, "CKT-41")
    assert "CKT-41" in out
    assert "Beeline" in out
    assert "Burst circuits" in out
    assert iface_name == "ae5.0"
    assert " n/a " not in row
    assert "90.00000" in row


def test_ertelecom_burst_sla_report_circuit_42(tmp_path, monkeypatch, capsys, zabbix_env):
    iface_name = "ae3.0"
    nb = build_netbox_for_commit_rates(
        device_name="MSK-M9-MX204-2",
        iface_name="et-0/0/3",
        provider_name="Ertelecom",
        circuit_id=42,
        circuit_custom_fields={"billing_model": "Burst"},
        tag_device=False,
    )
    dry_ssh = tmp_path / "dry-ssh.json"
    dry_ssh.write_text(json.dumps({"devices": MSK_MX204_2_DRY_SSH}), encoding="utf-8")
    _zabbix_burst_mocker("MSK-M9-MX204-2", iface_name, "CKT-42").activate(monkeypatch)

    with patch("zabbix_provider_services.netbox_client_from_env", return_value=nb), patch(
        "zabbix_provider_services.netbox_border_tag", return_value=None
    ):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "zabbix_provider_sla.py",
                "-f",
                str(tmp_path / "ignored.json"),
                "--days",
                "1",
                "--from-ts",
                "0",
                "--to-ts",
                "10000",
                "--dry-ssh",
                str(dry_ssh),
            ],
        )
        sla_mod.main()

    out = capsys.readouterr().out
    row = _burst_row_for_circuit(out, "CKT-42")
    assert "CKT-42" in out
    assert "Ertelecom" in out
    assert " n/a " not in row
    assert "90.00000" in row


def test_fiord_burst_sla_report_circuit_52(tmp_path, monkeypatch, capsys, zabbix_env):
    nb = build_netbox_for_commit_rates(
        device_name="WAW-EQX-7280QR-2",
        iface_name="Ethernet34/1",
        provider_name="Fiord",
        circuit_id=52,
        circuit_custom_fields={"billing_model": "Burst"},
        tag_device=False,
    )
    _zabbix_burst_mocker("WAW-EQX-7280QR-2", "Ethernet34/1", "CKT-52").activate(monkeypatch)

    with patch("zabbix_provider_services.netbox_client_from_env", return_value=nb), patch(
        "zabbix_provider_services.netbox_border_tag", return_value=None
    ):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "zabbix_provider_sla.py",
                "-f",
                str(tmp_path / "ignored.json"),
                "--days",
                "1",
                "--from-ts",
                "0",
                "--to-ts",
                "10000",
            ],
        )
        sla_mod.main()

    out = capsys.readouterr().out
    row = _burst_row_for_circuit(out, "CKT-52")
    assert "CKT-52" in out
    assert "Fiord" in out
    assert " n/a " not in row
    assert "90.00000" in row


def test_runtime_slo_uses_project_config_not_commit_rates(tmp_path, monkeypatch, capsys, zabbix_env):
    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
        provider_name="ManualISP",
        circuit_id=55,
        circuit_custom_fields={"billing_model": "Burst"},
        tag_device=False,
    )
    cr = tmp_path / "commit_rates.json"
    cr.write_text(json.dumps({"_provider_sla": 99.0}), encoding="utf-8")

    agg = sla_mod.UPLINKS_AGGREGATE_HOST_PREFIX + "ManualISP"
    (
        ZabbixRpcMocker()
        .on("host.get", lambda p: [{"hostid": "50", "host": agg, "name": agg}])
        .on(
            "trigger.get",
            lambda p: [
                {
                    "triggerid": "agg-sla",
                    "description": "Provider aggregate traffic >= 100% of limit",
                    "hosts": [{"hostid": "50"}],
                },
            ],
        )
        .on(
            "event.get",
            lambda p: [
                {"clock": "0", "value": "1"},
                {"clock": "9999", "value": "0"},
            ],
        )
        .activate(monkeypatch)
    )

    with patch("zabbix_provider_services.netbox_client_from_env", return_value=nb), patch(
        "zabbix_provider_services.netbox_border_tag", return_value=None
    ):
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
                "10000",
            ],
        )
        sla_mod.main()

    out = capsys.readouterr().out
    project_slo = getattr(uplinks_config, "PROJECT_PROVIDER_SLO_PERCENT", 99.95)
    assert "PROJECT_PROVIDER_SLO_PERCENT" in out
    assert "{:.5f}".format(project_slo) in out
    assert "Target SLA (_provider_sla)" not in out


def test_main_auth_denied_exits_without_legacy_json_fallback(
    tmp_path, monkeypatch, capsys, zabbix_env
):
    cr = tmp_path / "commit_rates.json"
    cr.write_text(
        json.dumps({"_provider_limits": {"Cogent": 10}, "_provider_sla": 99.0}),
        encoding="utf-8",
    )

    def fake_load_ctx(debug=False):
        return {
            "read_error": True,
            "stats": {"error": sla_mod.ERROR_AUTH_DENIED},
            "providers": set(),
            "burst_circuits": [],
            "provider_slo_percent": {},
            "provider_limits_gbps": {},
            "report": {"stats": {"error": sla_mod.ERROR_AUTH_DENIED}, "complete": [], "incomplete": []},
        }

    monkeypatch.setattr(sla_mod, "_load_netbox_services_context", fake_load_ctx)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "zabbix_provider_sla.py",
            "-f",
            str(cr),
            "--legacy-commit-rates-fallback",
            "--days",
            "1",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        sla_mod.main()
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "NetBox error" in captured.err
    assert "Cogent" not in captured.out


def test_resolve_provider_slo_order():
    ctx = {
        "providers": {"Cogent", "ManualISP"},
        "provider_slo_percent": {"Cogent": 99.9},
    }
    project_slo = getattr(uplinks_config, "PROJECT_PROVIDER_SLO_PERCENT", 99.95)
    assert sla_mod._resolve_provider_slo("Cogent", ctx, project_slo=project_slo) == 99.9
    assert sla_mod._resolve_provider_slo("ManualISP", ctx, project_slo=project_slo) == project_slo
